#!/usr/bin/env python3
"""Delete the pages the index is holding twice, and nothing else.

The compaction agent exists and is tested, but nothing in production ever
called it, so a page stored under both `ksrtc.in/fare` and
`www.ksrtc.in/fare` stayed twice: two rows against the host's page quota,
two cards in the result page, both opening the same URL.

    # see what it would remove, change nothing
    python3 scripts/compact_index.py

    # remove it
    python3 scripts/compact_index.py --yes

Two passes, in this order: identical bodies first (the cheapest, largest win),
then the same host and path once `www.`, the query string and a trailing slash
are set aside. The copy with the higher quality score survives; the newer row
breaks a tie.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.agents import load_agent  # noqa: E402
from app.postgres import PostgresClient  # noqa: E402

DedupeCompactAgent = load_agent("13_dedupe_compact_agent").DedupeCompactAgent


async def main() -> int:
    parser = argparse.ArgumentParser(description="Remove duplicate pages.")
    parser.add_argument(
        "--yes",
        action="store_true",
        help="actually delete; without it the run only counts what it would remove",
    )
    parser.add_argument("--dsn", default=None, help="override DATABASE_URL")
    args = parser.parse_args()

    dsn = args.dsn or os.environ.get("DATABASE_URL", "")
    if not dsn:
        print("  DATABASE_URL is not set", file=sys.stderr)
        return 2

    dry_run = not args.yes
    client = PostgresClient(dsn)
    try:
        before = (await client.execute("SELECT count(*) FROM pages")).scalar(0)
        agent = DedupeCompactAgent(client)
        report = await agent.run(dry_run=dry_run)
        after = (await client.execute("SELECT count(*) FROM pages")).scalar(0)

        print(f"  identical bodies   : {report.duplicate_digests}")
        print(f"  same host and path : {report.duplicate_paths}")
        if dry_run:
            print(f"  dry run: {report.pages_removed} pages would be removed; index holds {before}")
            print("  re-run with --yes to remove them")
        else:
            print(f"  removed {before - after} pages; index holds {after}")
    finally:
        await client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
