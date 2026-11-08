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
from app.crawler.breaker import DomainCircuitBreaker
from app.engine.config import Settings, get_settings
from app.engine.store import IndexStore, PageDocument, QueuedUrl
from app.indexer.guard import StorageGuard
from app.libsql_http import LibsqlClient

#: The three ceilings, and the three functions that enforce them. Imported from
#: the agents that own the rules rather than reimplemented here.
_parser = load_agent("03_parser_agent")
_brand = load_agent("10_brand_canonical_agent")
_indexer = load_agent("05_indexer_agent")
format_company_bounded_title = _parser.format_company_bounded_title
sanitize_brand = _brand.sanitize_brand
brand_from_domain = _brand.brand_from_domain
format_ai_snippet = _indexer.format_ai_snippet
clean_description = load_agent("04_quality_agent").clean_description
get_host_hard_limit = load_agent("01_frontier_agent").get_host_hard_limit
TITLE_CHAR_CAP = _parser.TITLE_CHAR_CAP
BRAND_CHAR_CAP = _parser.BRAND_CHAR_CAP
DESCRIPTION_CHAR_CAP = _parser.DESCRIPTION_CHAR_CAP

logger = logging.getLogger(__name__)

#: When the frontier is dry and nothing can be re-armed yet, back off longer
#: than the ordinary idle sleep so the worker is not polling the database
#: every 15 seconds for the whole of a six-hour cooldown.
IDLE_REFILL_FAILED_BACKOFF_SECONDS = 60.0

#: Backoff after an unexpected error escapes a batch. Long enough that a
#: persistent bug cannot spin the loop and flood the logs, short enough that a
#: transient fault does not look like a dead crawler: 60s was indistinguishable
#: from a stall.
LOOP_ERROR_BACKOFF_SECONDS = 5.0

#: Batches over which the frontier-growth check measures progress. Ten batches
#: is long enough that one slow or empty pass does not trip it, and short enough
#: to see a real stall before the storage guard has to pause.
FRONTIER_STALL_WINDOW = 10

#: Empty batches in a row that mean the queue holds no work this run can do.
#: `FrontierGrowthWatch` needs discovery to outpace closure to notice a stall;
#: a queue full of rows whose hosts are all at their ceiling discovers nothing
#: and closes nothing, so it looks idle rather than stuck.
IDLE_QUEUE_STALL_BATCHES = 10

#: Note on frequency: the reconcile is an UPDATE over the pending backlog, and
#: on a small VM it competes with `pending_batch()` for the same rows. Running
#: it on a timer made both queries hit the statement timeout, so it is triggered
#: by idleness only — the state it exists to repair.


class FrontierGrowthWatch:
    """Warns when the frontier grows while indexing makes no progress.

    The symptom this catches: discovery keeps admitting URLs, nothing reaches
    the index, and the queue climbs until the storage guard pauses the crawler.
    By then the cause is buried. This says so in the log while it is happening.

    It uses only what `BatchOutcome` already reports — links discovered and
    queue rows closed — so it costs no extra database round trip.
    """

    def __init__(self, window: int = FRONTIER_STALL_WINDOW) -> None:
        self._window = max(1, window)
        self._reset()

    def _reset(self) -> None:
        self._batches = 0
        self._indexed = 0
        self._discovered = 0
        self._closed = 0

    def observe(self, *, indexed: int, discovered: int, closed: int) -> str | None:
        """Record one batch; return a warning once per window when stalled."""
        self._batches += 1
        self._indexed += max(0, indexed)
        self._discovered += max(0, discovered)
        self._closed += max(0, closed)

        if self._batches < self._window:
            return None

        growth = self._discovered - self._closed
        message: str | None = None
        if self._indexed == 0 and growth > 0:
            message = (
                "frontier growing while indexing is stalled: "
                f"{self._discovered} discovered, {self._closed} queue rows closed, "
                f"0 pages indexed over {self._batches} batches"
            )

        self._reset()
        return message


class IdleQueueWatch:
    """Decides when an empty frontier is a stall rather than a quiet moment.

    `run_batch()` returns an empty outcome when the queue holds no row this run
    may fetch: every host is at its page ceiling, or the rows are out of crawl
    scope. The crawler then spins, the queue depth stays high, and nothing in
    the log says why — the index simply stops growing while `/health` reports a
    healthy worker. This is the alarm for exactly that state.
    """

    def __init__(self, window: int = IDLE_QUEUE_STALL_BATCHES) -> None:
        self._window = max(1, window)
        self._empty = 0

    def observe(self, *, empty_batch: bool) -> bool:
        """Record one batch; return True once per window of empty batches."""
        if not empty_batch:
            self._empty = 0
            return False
        self._empty += 1
        if self._empty < self._window:
            return False
        self._empty = 0
        return True


#: How often the loop prunes terminal queue rows. Every batch would spend a
#: round trip on housekeeping; this is often enough to stay ahead of growth.
QUEUE_PRUNE_EVERY_BATCHES = 100

_frontier_module = load_agent("01_frontier_agent")
_fetcher_module = load_agent("02_fetcher_agent")
_parser_module = load_agent("03_parser_agent")
_quality_module = load_agent("04_quality_agent")
_indexer_module = load_agent("05_indexer_agent")
_revalidation_module = load_agent("06_revalidation_agent")
_classifier_module = load_agent("07_classifier_agent")
_language_module = load_agent("08_language_agent")
_robots_module = load_agent("09_robots_agent")

FrontierAgent = _frontier_module.FrontierAgent
FetcherAgent = _fetcher_module.FetcherAgent
ParserAgent = _parser_module.ParserAgent
QualityAgent = _quality_module.QualityAgent
IndexerAgent = _indexer_module.IndexerAgent
RevalidationAgent = _revalidation_module.RevalidationAgent
ClassifierAgent = _classifier_module.ClassifierAgent
LanguageAgent = _language_module.LanguageAgent
RobotsCache = _robots_module.RobotsCache


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
    #: Links found on a parsed page and deliberately not queued, because the
    #: crawler is draining the repair backlog instead of growing the frontier.
    drain_dropped: int = 0
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
            limit=self._settings.effective_storage_page_limit,
            ceiling_bytes=self._settings.storage_ceiling_bytes,
            ttl_seconds=self._settings.storage_count_ttl_seconds,
        )
        self._fetcher = FetcherAgent(self._settings, transport=transport)
        self._parser = ParserAgent()
        self._quality = QualityAgent()
        self._classifier = ClassifierAgent()
        self._language = LanguageAgent()
        self._indexer = IndexerAgent(self._store, self._guard)
        self._growth_watch = FrontierGrowthWatch()
        self._robots = (
            RobotsCache(
                self._fetcher.client,
                user_agent=self._settings.crawl_user_agent,
                timeout=self._settings.robots_timeout_seconds,
                max_entries=self._settings.robots_cache_entries,
                max_concurrent=self._settings.robots_max_concurrent,
            )
            if self._settings.robots_enabled
            else None
        )
        # The fetcher spaces requests per host; the host's own Crawl-delay is
        # the instruction that spacing should obey.
        self._fetcher.robots = self._robots
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
        # Domains that stopped answering during this run. Per-run state on
        # purpose: a restart should get to form its own opinion.
        self._breaker = DomainCircuitBreaker(
            threshold=self._settings.crawl_breaker_failure_threshold,
            cooldown=self._settings.crawl_breaker_cooldown_seconds,
        )
        # When each domain was last asked, for per-domain politeness.
        self._last_request: dict[str, float] = {}
        self._pending_sitemap_domains: set[str] = set()
        self._sitemap_harvest_task: asyncio.Task[None] | None = None

    #: Set by the storage watchdog when the database reaches its ceiling. While
    #: it is set the crawl loop takes no new work; revalidation keeps running.
    freshness_only: bool = False

    #: The supervisor, when one is running. The loops beat into it so a loop
    #: that is alive but stuck can be told apart from one that is working.
    king = None

    #: The autosuggest trie, once the maintenance loop has built it.
    autosuggest = None
    #: The federated fleet, wired by the scheduler when federation is enabled.
    federated = None
    marshal = None

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
        retryable_failures: dict[str, list[int]] = {}
        skipped_ids: list[int] = []
        discovered: list[str] = []

        # Admission: a URL whose canonical form is already indexed is not work.
        # `www.ksrtc.in/fare` and `ksrtc.in/fare` are one page; fetching the
        # second costs a fetch, a parse and one page of the host's quota, and
        # then lands in the index as a duplicate the reader sees twice.
        canonical_of = {
            item.rowid: _frontier_module.normalise_url(item.url) for item in queue
        }
        already_indexed = await self._store.indexed_urls(
            [canonical for canonical in canonical_of.values() if canonical]
        )
        if already_indexed:
            kept: list[QueuedUrl] = []
            for item in queue:
                canonical = canonical_of.get(item.rowid)
                if canonical and canonical in already_indexed:
                    outcome.skipped += 1
                    self._tally(outcome.skip_reasons, "already_indexed")
                    skipped_ids.append(item.rowid)
                else:
                    kept.append(item)
            queue = kept
            if not queue:
                await self._indexer.park(failed_ids, skipped_ids)
                return outcome

        # Per-host ceiling: one count query for the whole batch, and URLs on a
        # host that has reached its tier's limit are parked before any fetch
        # happens. The ceiling is a stop, not a quota to fill — a host with three
        # good pages is finished at three, whatever its tier allows.
        counts = await self._store.domain_page_counts([item.domain for item in queue])
        eligible: list[QueuedUrl] = []
        for item in queue:
            if counts.get(item.domain, 0) >= get_host_hard_limit(item.domain):
                outcome.skipped += 1
                self._tally(outcome.skip_reasons, "domain_cap")
                skipped_ids.append(item.rowid)
            else:
                eligible.append(item)

        # A domain whose breaker is open is not asked again. Its queued rows are
        # parked rather than fetched: a hundred URLs on a dead host used to cost
        # a hundred timeouts to learn one fact.
        still_eligible: list[QueuedUrl] = []
        for item in eligible:
            if self._breaker.is_open(item.domain):
                outcome.skipped += 1
                self._tally(outcome.skip_reasons, "circuit_open")
                skipped_ids.append(item.rowid)
                if self._breaker.should_report(item.domain):
                    logger.warning(
                        "domain %s is not answering; parking its queued URLs for %.0fs",
                        item.domain,
                        self._breaker.cooldown,
                    )
            else:
                still_eligible.append(item)
        eligible = still_eligible

        # robots.txt is checked per host, cached for an hour, and fetched
        # concurrently: a batch of 120 URLs across 100 hosts would otherwise
        # serialise at up to 2s each. A refusal parks the URL like a cap skip.
        if self._robots is not None and eligible:
            verdicts = await asyncio.gather(
                *[self._robots.allowed(item.url) for item in eligible]
            )
            permitted: list[QueuedUrl] = []
            for item, is_allowed in zip(eligible, verdicts):
                if is_allowed:
                    permitted.append(item)
                else:
                    outcome.skipped += 1
                    self._tally(outcome.skip_reasons, "robots_disallow")
                    skipped_ids.append(item.rowid)
            eligible = permitted

        self._quality.reset()
        results = await self._fetcher.fetch_many([item.url for item in eligible])

        for item, result in zip(eligible, results):
            # A host can reach its ceiling part-way through a batch.
            if counts.get(item.domain, 0) >= get_host_hard_limit(item.domain):
                outcome.skipped += 1
                self._tally(outcome.skip_reasons, "domain_cap")
                skipped_ids.append(item.rowid)
                continue

            if not result.ok:
                outcome.rejected += 1
                reason = result.reason or "fetch_failed"
                # Only a transport failure counts against a domain. A 404 is
                # the server answering; it says the page is gone, not that the
                # host is down, and opening the breaker on it would park a
                # healthy domain.
                if reason in ("timeout", "connect_error", "connection_error") or reason.startswith("gather_error"):
                    if self._breaker.record_failure(item.domain):
                        logger.warning(
                            "domain %s failed %d times in a row; opening its breaker",
                            item.domain,
                            self._breaker.threshold,
                        )
                self._tally(outcome.skip_reasons, reason)
                if (
                    reason == "timeout"
                    or reason.startswith("http_error:")
                    or reason.startswith("http_status:5")
                ):
                    retryable_failures.setdefault(reason, []).append(item.rowid)
                else:
                    failed_ids.append(item.rowid)
                continue

            outcome.fetched += 1
            self._breaker.record_success(item.domain)
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
                    parsed.links[: self._settings.max_outbound_links_per_page],
                    item.url,
                    allow_global=self._settings.allow_global_domains,
                )
            )

            # Nothing unvalidated reaches the store. The parser produces a
            # title, a brand and a description from a page it did not write, and
            # every one of them can be a domain suffix, a modal's button label or
            # a 400-character paragraph. The caps are applied here as well as in
            # the parser because a page that slips past one check should not slip
            # past both.
            clean_brand = sanitize_brand(parsed.brand_name or brand_from_domain(item.url))
            bounded_title = format_company_bounded_title(
                parsed.title, clean_brand, h1_text=parsed.action_title, url=item.url
            )
            # A clean description is kept exactly as it is, however short. The
            # body is read only when there is no description left to keep.
            clean_desc = clean_description(
                parsed.description, parsed.body_text, bounded_title
            )

            document = PageDocument(
                url=item.url,
                domain=item.domain,
                title=bounded_title[:TITLE_CHAR_CAP],
                description=clean_desc[:DESCRIPTION_CHAR_CAP],
                body_text=parsed.body_text,
                schema_types=list(parsed.schema_types),
                sitelinks=list(parsed.sitelinks),
                brand_name=clean_brand[:BRAND_CHAR_CAP],
                clean_url=parsed.clean_url,
                action_title=bounded_title[:TITLE_CHAR_CAP],
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
                schema_types=document.schema_types,
            ).category
            document.language = self._language.detect(
                f"{document.title} {document.body_text}"
            )

            documents.append(document)
            queue_ids.append(item.rowid)
            counts[item.domain] = counts.get(item.domain, 0) + 1

        outcome.accepted = len(documents)

        for reason, rowids in retryable_failures.items():
            outcome.queue_failed += await self._store.mark_retryable_failed(rowids, reason)

        if failed_ids or skipped_ids:
            failed, _skipped = await self._indexer.park(failed_ids, skipped_ids)
            outcome.queue_failed += failed
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

        # Drain mode: the frontier is being emptied on purpose, so a parsed page
        # contributes nothing. Its links are dropped rather than queued — that is
        # the whole point, and it is why the repair rows get fetched instead of
        # competing with new discovery for the same single crawl slot.
        if self._settings.crawler_drain_mode and discovered:
            outcome.drain_dropped += len(discovered)
            discovered = []

        if discovered and not outcome.paused:
            # Do not grow the frontier with URLs whose domain is already full.
            discovered = [
                url
                for url in discovered
                if counts.get(_frontier_module.domain_of(url), 0)
                < get_host_hard_limit(_frontier_module.domain_of(url))
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
        if self._settings.crawler_drain_mode:
            # A sitemap is the largest source of new URLs there is. Reading one
            # while draining would refill the frontier we are trying to empty.
            return
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
        # A previous version could re-arm processed URLs from domains already
        # at the page cap. pending_batch() correctly excludes those domains,
        # but queue_depth() still counted their pending rows; that made the
        # frontier look non-empty forever while no URL was eligible to crawl.
        # Reconcile that stale backlog once when the worker starts.
        try:
            # No cap argument: the reconciliation measures each host against
            # its own tier's ceiling.
            parked = await self._store.skip_pending_for_capped_domains()
            if parked:
                logger.warning(
                    "startup frontier repair: parked %d pending URLs from capped domains",
                    parked,
                )
        except Exception:
            logger.exception("startup frontier repair failed; crawler will continue")

        batches_since_prune = 0
        idle_watch = IdleQueueWatch()

        while True:
            if self.king is not None:
                self.king.beat("crawler")
            # The whole iteration is guarded, not just the batch. `refill()`
            # does network and database work of its own, and an exception from
            # it used to escape this loop and end the worker for good — which
            # is indistinguishable from a crawl that has simply stopped.
            try:
                # Freshness-only: the database is at its ceiling, so stop taking
                # new URLs and let the revalidation worker spend the effort on
                # the pages already held. The watchdog sets this.
                if self.freshness_only:
                    await asyncio.sleep(self._settings.crawl_empty_backoff_seconds)
                    continue

                outcome = await self.run_batch()

                # A batch has just changed what the index holds, so any cached
                # answer mentioning one of those pages is now stale. Expiring
                # only the affected entries keeps every other query warm.
                service = getattr(self, "search_service", None)
                if service is not None and outcome is not None:
                    touched = getattr(outcome, "urls", None) or []
                    if touched:
                        try:
                            service.invalidate(touched)
                        except Exception:  # noqa: BLE001 - a stale cache is not fatal
                            logger.debug("cache invalidation failed", exc_info=True)

                # Recover the frontier on EVERY pass, including successful
                # batches. Previously the non-empty fast path continued before
                # refill/retry ran, so recovery silently depended on the queue
                # becoming empty. Re-arm work and refill before deciding to
                # sleep; INSERT OR IGNORE and cooldowns keep this bounded.
                rearmed = await self._store.rearm_retryable_failed(limit=25)
                if rearmed:
                    logger.info("re-armed %d transient crawler failures", rearmed)

                added = await self._frontier.refill(minimum=1)
                if added:
                    logger.info("crawler frontier refilled with %d URLs", added)

                # An empty batch while the queue still holds rows means the rows
                # are not work: their host is at its ceiling or out of scope.
                # Park them so the depth tells the truth, and say why the crawl
                # has gone quiet instead of leaving it to be discovered days
                # later from a page count that stopped moving.
                stalled = idle_watch.observe(empty_batch=outcome is not None and outcome.empty)
                if stalled:
                    parked = await self._store.skip_pending_for_capped_domains()
                    depth = await self._store.queue_depth()
                    if parked:
                        logger.warning(
                            "parked %d pending URLs whose host is at its page ceiling "
                            "(queue depth %d)",
                            parked,
                            depth,
                        )
                    if stalled and depth:
                        logger.warning(
                            "crawl idle for %d empty batches with %d pending URLs: none of "
                            "them is eligible — every host is at its ceiling or the rows are "
                            "out of crawl scope. Queue new domains to grow the index.",
                            IDLE_QUEUE_STALL_BATCHES,
                            depth,
                        )

                # Early warning, before the storage guard has to pause anything.
                if outcome is not None:
                    stall = self._growth_watch.observe(
                        indexed=outcome.indexed_pages,
                        discovered=outcome.discovered,
                        closed=outcome.queue_closed,
                    )
                    if stall:
                        logger.warning("%s", stall)

                # Keep crawl_queue lean. Bounded per call, so this is a slow
                # steady drip rather than one large delete.
                batches_since_prune += 1
                if batches_since_prune >= QUEUE_PRUNE_EVERY_BATCHES:
                    batches_since_prune = 0
                    pruned = await self._store.prune_queue(
                        retain_days=self._settings.queue_retain_days,
                        limit=self._settings.queue_prune_limit,
                    )
                    if pruned:
                        logger.info("pruned %d terminal queue rows", pruned)

                if outcome is not None and not outcome.empty and not outcome.paused:
                    await asyncio.sleep(self._settings.crawl_batch_sleep_seconds)
                    continue

                delay = self._settings.crawl_empty_backoff_seconds
                if outcome is not None and outcome.empty and not added and not rearmed:
                    # Nothing to crawl and nothing past its cooldown yet.
                    delay = max(delay, IDLE_REFILL_FAILED_BACKOFF_SECONDS)

                logger.debug("crawl worker sleeping %.1fs (frontier idle)", delay)
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
