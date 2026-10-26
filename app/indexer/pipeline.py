"""The ingestion pipeline.

Two entry points matter:

* `crawl_and_index(seeds)` — fetch the seed domains and index what comes back.
* `process_backlog()` — take the queries the index could not answer and crawl
  the seed that looks relevant to them, so the next identical query is answered
  from our own index.

Run it from a scheduler or by hand:

    python -m app.indexer.pipeline --seeds
    python -m app.indexer.pipeline --backlog
    python -m app.indexer.pipeline --query "namma metro timings"
"""

from __future__ import annotations

import argparse
import asyncio
import logging

from app import database
from app.crawler.crawler import crawl_seeds
from app.crawler.seeds import SEED_DOMAINS, Seed
from app.engine.config import get_engine_settings
from app.engine.store import get_store
from app.engine.text import ExtractedDocument
from app.indexer.revalidate import revalidate_batch

logger = logging.getLogger(__name__)


async def index_documents(documents: list[ExtractedDocument]) -> dict[str, int]:
    """Ingest extracted documents; duplicates and over-cap domains are rejected."""
    if not documents:
        return {"inserted": 0}
    store = get_store()
    outcome = await store.ingest_documents([doc.as_row() for doc in documents])
    logger.info("ingest outcome: %s", outcome)
    return outcome


async def crawl_and_index(seeds: list[Seed] | None = None) -> dict[str, int]:
    """Crawl the given seeds (default: the whole seed list) and index the result."""
    settings = get_engine_settings()
    if not settings.crawl_enabled:
        logger.info("crawling disabled (CRAWL_ENABLED=false); nothing to do")
        return {"inserted": 0}

    results = await crawl_seeds(list(seeds or SEED_DOMAINS), settings=settings)
    documents = [doc for result in results for doc in result.documents]
    return await index_documents(documents)


def _seeds_for_query(query: str) -> list[Seed]:
    """Pick the seeds that plausibly cover a backlog query."""
    tokens = {token for token in query.lower().split() if len(token) > 2}
    if not tokens:
        return list(SEED_DOMAINS[:3])

    matches = [
        seed
        for seed in SEED_DOMAINS
        if tokens & {word for word in f"{seed.label} {seed.url}".lower().replace("/", " ").replace(".", " ").split() if len(word) > 2}
    ]
    # Nothing matched: crawl a small slice of the general seeds rather than all.
    return matches or list(SEED_DOMAINS[:3])


async def process_backlog(limit: int = 5) -> dict[str, int]:
    """Crawl for the queries the index could not answer, then mark them done."""
    store = get_store()
    queries = await store.pending_backlog(limit)
    processed = 0
    indexed = 0

    for query in queries:
        seeds = _seeds_for_query(query)
        logger.info("backlog %r → crawling %s", query, [seed.url for seed in seeds])
        outcome = await crawl_and_index(seeds)
        indexed += int(outcome.get("inserted", 0))
        await store.mark_backlog_processed(query)
        processed += 1

    return {"queries": processed, "documents": indexed}


async def _main() -> None:
    parser = argparse.ArgumentParser(description="EyeSearch indexer")
    parser.add_argument("--seeds", action="store_true", help="crawl and index the seed domains")
    parser.add_argument("--backlog", action="store_true", help="crawl for unanswered queries")
    parser.add_argument("--query", type=str, default=None, help="crawl the seeds relevant to one query")
    parser.add_argument("--stats", action="store_true", help="print index statistics")
    parser.add_argument("--revalidate", action="store_true", help="run one stale-while-revalidate pass")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s :: %(message)s")

    from app.config import get_settings

    settings = get_settings()
    await database.connect(settings)
    try:
        await database.initialize_schema()
        store = get_store()
        await store.ensure_schema()

        if args.stats:
            print(await store.stats())
        if args.query:
            print(await crawl_and_index(_seeds_for_query(args.query)))
        if args.backlog:
            print(await process_backlog())
        if args.revalidate:
            print(await revalidate_batch())
        if args.seeds:
            print(await crawl_and_index())
    finally:
        await database.close()


if __name__ == "__main__":
    asyncio.run(_main())
