"""The orchestrator: frontier → fetcher → parser → quality → indexer.

Each stage runs as its own coroutine reading from an `asyncio.Queue`, so a slow
or failing stage applies backpressure instead of corrupting the others. A
failure anywhere is caught, logged and counted — it never propagates to the
search API, which shares this process.

    orchestrator = Orchestrator()
    report = await orchestrator.run(hosts=["eci.gov.in", "ssc.nic.in"])

`run_from_frontier()` pulls its own work from the frontier queue instead of a
list you supply.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Iterable

from app.agents import (
    FetcherAgent,
    FrontierAgent,
    IndexerAgent,
    ParserAgent,
    QualityAgent,
    SemanticAIAgent,
)

logger = logging.getLogger(__name__)

# One in-flight page per slot; the fetcher applies its own concurrency limit.
QUEUE_SIZE = 32


@dataclass
class OrchestratorReport:
    """What one run did, stage by stage."""

    attempted: int = 0
    fetched: int = 0
    parsed: int = 0
    accepted: int = 0
    rejected: int = 0
    indexed_pages: int = 0
    sites: int = 0
    errors: dict[str, int] = field(default_factory=dict)

    def fail(self, stage: str) -> None:
        self.errors[stage] = self.errors.get(stage, 0) + 1

    def as_dict(self) -> dict[str, Any]:
        return {
            "attempted": self.attempted,
            "fetched": self.fetched,
            "parsed": self.parsed,
            "accepted": self.accepted,
            "rejected": self.rejected,
            "indexed_pages": self.indexed_pages,
            "sites": self.sites,
            "errors": dict(self.errors),
        }


class Orchestrator:
    """Wires the six agents together over async queues."""

    def __init__(
        self,
        *,
        frontier: FrontierAgent | None = None,
        fetcher: FetcherAgent | None = None,
        parser: ParserAgent | None = None,
        quality: QualityAgent | None = None,
        indexer: IndexerAgent | None = None,
        semantic: SemanticAIAgent | None = None,
        store: Any | None = None,
        settings: Any | None = None,
    ) -> None:
        self.frontier = frontier or FrontierAgent(store=store)
        self.fetcher = fetcher or FetcherAgent(settings)
        self.parser = parser or ParserAgent(settings)
        self.quality = quality or QualityAgent(settings)
        self.indexer = indexer or IndexerAgent(store=store, settings=settings)
        self.semantic = semantic or SemanticAIAgent(settings)
        self.settings = settings

    # -- one page, all five stages ------------------------------------
    async def process_url(self, url: str, report: OrchestratorReport) -> Any | None:
        """Fetch → parse → quality for one URL. Returns the accepted document."""
        report.attempted += 1

        fetched = await self.fetcher.fetch(url)
        if not fetched.ok:
            report.fail("fetch")
            return None
        report.fetched += 1

        parsed = await self.parser.parse(url, fetched.html, status=fetched.status)
        if not parsed.ok:
            report.fail("parse")
            report.rejected += 1
            return None
        report.parsed += 1

        verdict = await self.quality.check(parsed.document)
        if not verdict.ok:
            report.rejected += 1
            report.fail(f"quality:{verdict.reason}")
            return None

        report.accepted += 1
        return parsed.document

    # -- a batch, with the queues in between --------------------------
    async def run(self, hosts: Iterable[str] | None = None, *, limit: int | None = None) -> dict[str, Any]:
        """Crawl `hosts` (or the frontier's next batch) through every stage."""
        report = OrchestratorReport()

        if hosts is None:
            hosts = await self.frontier.next_batch(limit)
        urls = [
            f"https://{host.strip().lower().removeprefix('www.')}/"
            for host in list(hosts)[: limit or None]
            if host and host.strip()
        ]
        if not urls:
            return report.as_dict()

        if not await self.indexer.can_write():
            logger.warning("orchestrator idle: storage safety threshold reached")
            return {**report.as_dict(), "paused": True}

        fetch_queue: asyncio.Queue = asyncio.Queue(maxsize=QUEUE_SIZE)
        parse_queue: asyncio.Queue = asyncio.Queue(maxsize=QUEUE_SIZE)
        index_queue: asyncio.Queue = asyncio.Queue(maxsize=QUEUE_SIZE)

        async def producer() -> None:
            for url in urls:
                await fetch_queue.put(url)
            for _ in range(3):
                await fetch_queue.put(None)

        async def fetch_stage() -> None:
            while True:
                url = await fetch_queue.get()
                if url is None:
                    await parse_queue.put(None)
                    return
                try:
                    fetched = await self.fetcher.fetch(url)
                except Exception as exc:  # isolation: one bad URL is not the run
                    logger.warning("fetch stage error for %s: %s", url, exc)
                    report.fail("fetch")
                    continue
                await parse_queue.put((url, fetched))

        async def parse_stage() -> None:
            while True:
                item = await parse_queue.get()
                if item is None:
                    await index_queue.put(None)
                    return
                url, fetched = item
                report.attempted += 1
                if not fetched.ok:
                    report.fail("fetch")
                    continue
                report.fetched += 1
                try:
                    parsed = await self.parser.parse(url, fetched.html, status=fetched.status)
                except Exception as exc:
                    logger.warning("parse stage error for %s: %s", url, exc)
                    report.fail("parse")
                    continue
                if not parsed.ok:
                    report.rejected += 1
                    report.fail(f"parse:{parsed.reason}")
                    continue
                report.parsed += 1
                await index_queue.put(parsed.document)

        async def index_stage() -> None:
            while True:
                document = await index_queue.get()
                if document is None:
                    return
                try:
                    verdict = await self.quality.check(document)
                except Exception as exc:
                    logger.warning("quality stage error: %s", exc)
                    report.fail("quality")
                    continue
                if not verdict.ok:
                    report.rejected += 1
                    report.fail(f"quality:{verdict.reason}")
                    continue
                report.accepted += 1
                try:
                    outcome = await self.indexer.persist([document])
                except Exception as exc:
                    logger.warning("index stage error: %s", exc)
                    report.fail("index")
                    continue
                report.indexed_pages += int(outcome.get("pages", 0))
                report.sites += int(outcome.get("sites", 0))

        try:
            async with self.fetcher:
                await asyncio.gather(producer(), fetch_stage(), parse_stage(), index_stage())
        except Exception as exc:  # pragma: no cover - defensive
            logger.error("orchestrator run failed: %s", exc)
            report.fail("orchestrator")

        logger.info("orchestrator run complete: %s", report.as_dict())
        return report.as_dict()

    async def run_from_frontier(self, *, limit: int | None = None) -> dict[str, Any]:
        """Take the frontier's next batch and run it through the pipeline."""
        hosts = await self.frontier.next_batch(limit)
        if not hosts:
            return OrchestratorReport().as_dict()
        report = await self.run(hosts, limit=limit)
        for host in hosts:
            await self.frontier.mark_done(host, status="processed" if report.get("fetched") else "failed")
        return report

    # -- the semantic seam -------------------------------------------
    async def answer(self, question: str, documents: list[dict[str, str]] | None = None) -> Any:
        """Agent 6's hook: a grounded answer, with the model wired later."""
        return await self.semantic.answer(question, documents)


__all__ = ["Orchestrator", "OrchestratorReport"]
