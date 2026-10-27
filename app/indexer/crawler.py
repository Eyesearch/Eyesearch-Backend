"""The URL pass, and a command line for running it by hand.

`run_url_pass` is deliberately thin: it asks the orchestrator for exactly one
batch. Everything that matters — the `rowid AS id` batch read, the atomic page
write plus queue transition, the confirmed-commit counter — lives in
`app/engine/store.py` and `app/orchestrator.py`, so there is one implementation
of each rule rather than a second one for the CLI.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from app.agents import load_agent
from app.database import initialize_database, read_counts, verify_writable
from app.engine.config import get_settings
from app.libsql_http import LibsqlClient, build_client
from app.orchestrator import BatchOutcome, Orchestrator

logger = logging.getLogger(__name__)

SEED_URLS = load_agent("01_frontier_agent").SEED_URLS


async def run_url_pass(orchestrator: Orchestrator, limit: int | None = None) -> BatchOutcome:
    """Process one batch of up to `limit` (default 10) queued URLs."""
    return await orchestrator.run_batch(limit=limit)


async def seed_frontier(orchestrator: Orchestrator) -> int:
    """Queue the seed URLs when the frontier is empty."""
    return await orchestrator.frontier.enqueue(SEED_URLS)


async def _probe(client: LibsqlClient) -> int:
    await initialize_database(client)
    counts = await read_counts(client)
    writable = await verify_writable(client)
    print(f"database: {counts}")
    print(f"writable: {writable}")
    return 0 if writable else 1


async def _main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="EyeSearch URL pass")
    parser.add_argument("--probe", action="store_true", help="check the database accepts writes")
    parser.add_argument("--refill", action="store_true", help="queue the seed URLs")
    parser.add_argument("--once", action="store_true", help="run a single batch")
    parser.add_argument("--limit", type=int, default=None, help="batch size (default 10)")
    parser.add_argument("--stats", action="store_true", help="print index counts")
    args = parser.parse_args(argv)

    settings = get_settings()
    logging.basicConfig(level=settings.log_level.upper())

    client = build_client(settings)
    try:
        await initialize_database(client)

        if args.probe:
            return await _probe(client)

        orchestrator = Orchestrator(client, settings)
        try:
            if args.stats:
                stats = await orchestrator.store.stats()
                print(stats.as_dict())
            if args.refill:
                added = await seed_frontier(orchestrator)
                print(f"queued {added} seed URLs")
            if args.once or not (args.probe or args.refill or args.stats):
                outcome = await run_url_pass(orchestrator, args.limit)
                print(outcome.as_dict())
        finally:
            await orchestrator.close()
        return 0
    finally:
        await client.close()


def main() -> None:
    try:
        raise SystemExit(asyncio.run(_main(sys.argv[1:])))
    except KeyboardInterrupt:
        raise SystemExit(130)


if __name__ == "__main__":
    main()


__all__ = ["SEED_URLS", "main", "run_url_pass", "seed_frontier"]
